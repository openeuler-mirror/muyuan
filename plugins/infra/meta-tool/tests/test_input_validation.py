"""Executor validation regressions shared with the current BATE runtime."""
import asyncio
import copy
from types import MappingProxyType

import pytest

from bate.runtime import MetaToolExecutor, MetaToolValidationError
from bate.spec import MetaToolSpec, StepSpec, input_binding, step_binding


def spec_for(value_schema, **root):
    return MetaToolSpec(
        name="validate_then_read", description="Validation regression fixture",
        input_schema={"type": "object", "properties": {"value": value_schema},
                      "required": ["value"], "additionalProperties": False, **root},
        steps=(StepSpec("first", "first", {"value": input_binding("value")}),
               StepSpec("second", "second", {"value": step_binding("first")})),
        output=step_binding("second"), metadata={"side_effect_free": True},
    )


def run(spec, inputs, calls, asynchronous):
    def primitive(**kwargs):
        calls.append(kwargs)
        return kwargs["value"]

    async def aprimitive(**kwargs):
        return primitive(**kwargs)

    handler = aprimitive if asynchronous else primitive
    executor = MetaToolExecutor({"first": handler, "second": handler})
    if asynchronous:
        return asyncio.run(executor.aexecute(spec, inputs))
    return executor.execute(spec, inputs)


INVALID = [
    ({"type": "string"}, 123, "$.value"),
    ({"type": "integer"}, True, "$.value"),
    ({"type": "integer"}, 1.5, "$.value"),
    ({"type": "number"}, "3", "$.value"),
    ({"type": "boolean"}, 1, "$.value"),
    ({"type": "string"}, None, "$.value"),
    ({"enum": ["red", "green"]}, "blue", "$.value"),
    ({"const": False}, 0, "$.value"),
    ({"type": "number", "minimum": 0}, -1, "$.value"),
    ({"maximum": 10}, 11, "$.value"),
    ({"exclusiveMinimum": 0}, 0, "$.value"),
    ({"exclusiveMaximum": 10}, 10, "$.value"),
    ({"multipleOf": 2}, 3, "$.value"),
    ({"minLength": 2}, "x", "$.value"),
    ({"maxLength": 2}, "xxx", "$.value"),
    ({"pattern": "^[A-Z]+$"}, "abc", "$.value"),
    ({"type": "array", "items": {"type": "integer"}}, [1, "2"], "$.value[1]"),
    ({"minItems": 1}, [], "$.value"),
    ({"maxItems": 1}, [1, 2], "$.value"),
    ({"uniqueItems": True}, [1, 1], "$.value"),
    ({"prefixItems": [{"type": "integer"}], "items": False}, [1, 2], "$.value"),
    ({"contains": {"const": "match"}}, ["other"], "$.value"),
    ({"type": "object", "required": ["id"]}, {}, "$.value"),
    ({"properties": {"id": {"type": "integer"}}, "additionalProperties": False},
     {"id": 1, "extra": 2}, "$.value"),
    ({"additionalProperties": {"type": "integer"}}, {"other": "x"}, "$.value.other"),
    ({"properties": {"rows": {"type": "array", "items": {"type": "object",
        "properties": {"count": {"type": "integer", "minimum": 0}}}}}},
     {"rows": [{"count": -1}]}, "$.value.rows[0].count"),
    ({"minProperties": 1}, {}, "$.value"),
    ({"maxProperties": 1}, {"a": 1, "b": 2}, "$.value"),
    ({"patternProperties": {"^id_": {"type": "integer"}}}, {"id_a": "x"}, "$.value.id_a"),
    ({"dependentRequired": {"a": ["b"]}}, {"a": 1}, "$.value"),
    ({"allOf": [{"type": "integer"}, {"minimum": 1}]}, 0, "$.value"),
    ({"anyOf": [{"type": "integer"}, {"type": "null"}]}, "x", "$.value"),
    ({"oneOf": [{"type": "integer"}, {"type": "number"}]}, 1, "$.value"),
    ({"not": {"const": "forbidden"}}, "forbidden", "$.value"),
    ({"if": {"type": "integer"}, "then": {"minimum": 10},
      "else": {"type": "null"}}, 1, "$.value"),
]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("schema,value,path", INVALID)
def test_constraints_block_all_primitives(schema, value, path, asynchronous):
    calls = []
    with pytest.raises(MetaToolValidationError) as raised:
        run(spec_for(schema), {"value": value}, calls, asynchronous)
    assert path in str(raised.value)
    assert calls == []


@pytest.mark.parametrize("inputs", [{}, {"value": "ok", "extra": True}, [], None])
def test_root_contract_is_checked(inputs):
    calls = []
    with pytest.raises(MetaToolValidationError):
        run(spec_for({"type": "string"}), inputs, calls, False)
    assert not calls


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"),
                                   object(), (1, 2), {1: "non-string-key"}])
def test_non_json_inputs_are_rejected_even_without_type_constraints(value):
    calls = []
    with pytest.raises(MetaToolValidationError):
        run(spec_for({}), {"value": value}, calls, False)
    assert not calls


def test_cyclic_input_does_not_execute():
    value = []
    value.append(value)
    calls = []
    with pytest.raises(MetaToolValidationError, match="cyclic"):
        run(spec_for({}), {"value": value}, calls, False)
    assert not calls


@pytest.mark.parametrize("schema,value", [
    ({"type": ["integer", "null"]}, None),
    ({"type": "number"}, 2),
    ({"enum": [False, 1]}, False),
    ({"type": "array", "items": {"type": "integer"}}, [1, 2]),
    ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, 2),
    ({"type": "object", "properties": {"count": {"type": "integer", "default": 3}}}, {}),
])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_valid_inputs_execute_unchanged(schema, value, asynchronous):
    inputs = MappingProxyType({"value": value})
    before = copy.deepcopy(value)
    calls = []
    result = run(spec_for(schema), inputs, calls, asynchronous)
    assert result.output == before
    assert value == before
    assert len(calls) == 2


def test_local_refs_and_schema_cache_follow_schema_content():
    spec = spec_for({"$ref": "#/$defs/value"}, **{"$defs": {"value": {"type": "integer"}}})
    calls = []
    assert run(spec, {"value": 2}, calls, False).output == 2
    spec.input_schema["$defs"]["value"]["minimum"] = 3
    calls.clear()
    with pytest.raises(MetaToolValidationError, match="minimum"):
        run(spec, {"value": 2}, calls, False)
    assert not calls


@pytest.mark.parametrize("schema,root", [
    ({"type": "unknown"}, {}),
    ({"minimum": "not-a-number"}, {}),
    ({}, {"$schema": "https://invalid.example/schema"}),
    ({}, {"$schema": {"invalid": "dialect"}}),
    ({"$ref": "#/$defs/missing"}, {}),
    ({"$ref": "https://invalid.example/schema"}, {}),
])
def test_invalid_or_unresolvable_schema_never_executes_or_fetches(schema, root, monkeypatch):
    import urllib.request

    def forbidden(*args, **kwargs):
        pytest.fail("input validation must not download a schema")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    calls = []
    with pytest.raises(MetaToolValidationError):
        run(spec_for(schema, **root), {"value": 1}, calls, False)
    assert not calls


def test_declared_draft_is_honored():
    spec = spec_for({"type": "array", "items": [{"type": "integer"}],
                     "additionalItems": False},
                    **{"$schema": "http://json-schema.org/draft-07/schema#"})
    calls = []
    assert run(spec, {"value": [1]}, calls, False).output == [1]
    calls.clear()
    with pytest.raises(MetaToolValidationError):
        run(spec, {"value": [1, 2]}, calls, False)
    assert not calls


def test_error_does_not_echo_argument_value():
    secret = "sensitive-example-value"
    with pytest.raises(MetaToolValidationError) as raised:
        run(spec_for({"type": "integer"}), {"value": secret}, [], False)
    assert secret not in str(raised.value)
    assert "$.value" in str(raised.value)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_missing_validation_dependency_blocks_all_primitives(monkeypatch, asynchronous):
    import builtins
    from bate.validation import _validator

    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "jsonschema":
            raise ModuleNotFoundError("jsonschema")
        return original(name, *args, **kwargs)

    _validator.cache_clear()
    monkeypatch.setattr(builtins, "__import__", unavailable)
    calls = []
    try:
        with pytest.raises(MetaToolValidationError, match="requires jsonschema"):
            run(spec_for({"type": "string"}), {"value": "valid"}, calls, asynchronous)
        assert not calls
    finally:
        _validator.cache_clear()
