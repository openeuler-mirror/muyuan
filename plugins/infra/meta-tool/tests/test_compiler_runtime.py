import asyncio

import pytest

from awo.runtime import MetaToolExecutionError, MetaToolExecutor
from awo.spec import MetaToolSpec, StepSpec, input_binding, step_binding


def _spec() -> MetaToolSpec:
    return MetaToolSpec(
        name="lookup_then_format",
        description="Look up an item and format its identifier.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        steps=(
            StepSpec(
                step_id="lookup",
                tool="lookup",
                arguments={"key": input_binding("key")},
            ),
            StepSpec(
                step_id="format",
                tool="format",
                arguments={
                    "identifier": step_binding("lookup", ("identifier",)),
                },
            ),
        ),
        output=step_binding("format"),
    )


def test_spec_round_trips_and_validates_bindings():
    spec = _spec()
    restored = MetaToolSpec.from_dict(spec.to_dict())

    assert restored == spec
    assert spec.as_tool_schema()["function"]["parameters"] == spec.input_schema


def test_sync_runtime_resolves_inputs_and_step_outputs():
    executor = MetaToolExecutor(
        {
            "lookup": lambda key: {"identifier": f"id:{key}"},
            "format": lambda identifier: {"value": identifier.upper()},
        }
    )

    result = executor.execute(_spec(), {"key": "alpha"})

    assert result.output == {"value": "ID:ALPHA"}
    assert [step.step_id for step in result.steps] == ["lookup", "format"]
    assert result.steps[1].arguments == {"identifier": "id:alpha"}


def test_async_runtime_awaits_async_primitive_tools():
    async def lookup(key):
        return {"identifier": f"id:{key}"}

    async def format_value(identifier):
        return {"value": identifier.upper()}

    result = asyncio.run(
        MetaToolExecutor({"lookup": lookup, "format": format_value}).aexecute(
            _spec(), {"key": "beta"}
        )
    )

    assert result.output == {"value": "ID:BETA"}


def test_runtime_rejects_missing_and_unknown_inputs():
    executor = MetaToolExecutor({"lookup": lambda key: {"identifier": key}})

    with pytest.raises(ValueError, match="required"):
        executor.execute(_spec(), {})
    with pytest.raises(ValueError, match="additionalProperties"):
        executor.execute(_spec(), {"key": "alpha", "extra": True})


def test_runtime_wraps_primitive_failures_with_completed_steps():
    def fail(identifier):
        raise RuntimeError(f"bad identifier: {identifier}")

    executor = MetaToolExecutor(
        {
            "lookup": lambda key: {"identifier": f"id:{key}"},
            "format": fail,
        }
    )

    with pytest.raises(MetaToolExecutionError) as raised:
        executor.execute(_spec(), {"key": "alpha"})

    assert raised.value.step_id == "format"
    assert [step.step_id for step in raised.value.completed_steps] == ["lookup"]
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_step_binding_falls_back_to_an_input_when_a_path_is_missing():
    spec = MetaToolSpec(
        name="wait_then_continue",
        description="Continue a pool after waiting for an event.",
        input_schema={
            "type": "object",
            "properties": {"pool_id": {"type": "string"}},
            "required": ["pool_id"],
        },
        steps=(
            StepSpec(
                step_id="wait",
                tool="wait",
                arguments={"pool_id": input_binding("pool_id")},
            ),
            StepSpec(
                step_id="continue",
                tool="continue_pool",
                arguments={
                    "pool_id": step_binding(
                        "wait",
                        ("events", 0, "pool_id"),
                        fallback=input_binding("pool_id"),
                    )
                },
            ),
        ),
        output=step_binding("continue"),
    )
    calls = []
    executor = MetaToolExecutor(
        {
            "wait": lambda pool_id: {"pool_id": pool_id, "events": []},
            "continue_pool": lambda pool_id: calls.append(pool_id)
            or {"pool_id": pool_id},
        }
    )

    result = executor.execute(spec, {"pool_id": "pool-1"})

    assert calls == ["pool-1"]
    assert result.output == {"pool_id": "pool-1"}
