import asyncio

import pytest

from bate.dispatch import (
    AccelerationPolicy,
    DispatchMode,
    MetaToolDispatcher,
    MetaToolRegistry,
    MetaToolRejectedError,
)
from bate.spec import MetaToolSpec, StepSpec, input_binding, step_binding


def _spec(**metadata):
    safe_metadata = {
        "reviewed": True,
        "requires_domain_review": False,
        "side_effect_free": True,
        "internal_data_flows": 1,
        "varying_data_flows": 1,
        "sequence_purity": 1.0,
        "min_branch_dominance": 1.0,
        "llm_turn_coverage": 1.0,
        "mean_llm_calls_saved_per_observed_occurrence": 1.0,
    }
    safe_metadata.update(metadata)
    return MetaToolSpec(
        name="read_then_format",
        description="Read a value and format it.",
        input_schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        steps=(
            StepSpec(
                step_id="read",
                tool="read",
                arguments={"key": input_binding("key")},
            ),
            StepSpec(
                step_id="format",
                tool="format",
                arguments={
                    "value": step_binding("read", ("value",)),
                },
            ),
        ),
        output=step_binding("format"),
        metadata=safe_metadata,
    )


def _primitives(events=None):
    events = events if events is not None else []

    def read(key):
        events.append(("read", key))
        return {"value": key.upper()}

    def format(value):
        events.append(("format", value))
        return f"[{value}]"

    return {"read": read, "format": format}


def test_safe_auto_dispatch_returns_only_final_output():
    telemetry = []
    dispatcher = MetaToolDispatcher(
        _primitives(),
        [_spec()],
        policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
        telemetry=telemetry.append,
    )
    output = dispatcher.dispatch("read_then_format", {"key": "alpha"})

    assert output == "[ALPHA]"
    assert dispatcher.last_event.path == "meta"
    assert dispatcher.last_event.primitive_steps == 2
    event = telemetry[0].to_dict()
    assert "output" not in event
    assert "arguments" not in event
    assert dispatcher.model_tool_schemas() == ()


def test_disabled_policy_transparently_uses_user_program_fallback():
    primitive_events = []
    dispatcher = MetaToolDispatcher(
        _primitives(primitive_events),
        [_spec()],
        policy=AccelerationPolicy(mode=DispatchMode.DISABLED),
        fallbacks={
            "read_then_format": lambda key: f"baseline:{key}",
        },
    )

    assert dispatcher.dispatch("read_then_format", {"key": "alpha"}) == (
        "baseline:alpha"
    )
    assert primitive_events == []
    assert dispatcher.last_event.path == "fallback"
    assert "disabled" in dispatcher.last_event.deopt_reason


def test_safe_auto_rejects_unreviewed_candidate_without_fallback():
    dispatcher = MetaToolDispatcher(
        _primitives(),
        [_spec(reviewed=False, requires_domain_review=True)],
        policy=AccelerationPolicy(mode="safe_auto"),
    )

    with pytest.raises(MetaToolRejectedError, match="domain review"):
        dispatcher.dispatch("read_then_format", {"key": "alpha"})
    assert dispatcher.last_event.path == "rejected"


def test_safe_auto_rejects_branching_or_compiler_warnings():
    branching = MetaToolDispatcher(
        _primitives(),
        [_spec(min_branch_dominance=0.75)],
        policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
    )
    with pytest.raises(MetaToolRejectedError, match="branch_dominance"):
        branching.dispatch("read_then_format", {"key": "alpha"})

    warned = MetaToolDispatcher(
        _primitives(),
        [_spec(compiler_warnings=["argument shape changed"])],
        policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
    )
    with pytest.raises(MetaToolRejectedError, match="compiler warnings"):
        warned.dispatch("read_then_format", {"key": "alpha"})


def test_safe_auto_rejects_missing_or_constant_only_data_flow():
    no_flow = _spec(internal_data_flows=0, varying_data_flows=0)
    no_flow = MetaToolSpec(
        name=no_flow.name,
        description=no_flow.description,
        input_schema=no_flow.input_schema,
        steps=(
            no_flow.steps[0],
            StepSpec(
                step_id="format",
                tool="format",
                arguments={"value": "fixed"},
            ),
        ),
        output=no_flow.output,
        metadata=no_flow.metadata,
    )
    missing = MetaToolDispatcher(
        _primitives(),
        [no_flow],
        policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
    )
    with pytest.raises(MetaToolRejectedError, match="no internal cross-step"):
        missing.dispatch("read_then_format", {"key": "alpha"})

    constant = MetaToolDispatcher(
        _primitives(),
        [_spec(varying_data_flows=0)],
        policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
    )
    with pytest.raises(MetaToolRejectedError, match="varying_data_flows"):
        constant.dispatch("read_then_format", {"key": "alpha"})


def test_force_mode_is_available_for_experiments():
    dispatcher = MetaToolDispatcher(
        _primitives(),
        [_spec(reviewed=False, requires_domain_review=True)],
        policy=AccelerationPolicy(mode=DispatchMode.FORCE),
    )

    assert dispatcher.dispatch("read_then_format", {"key": "alpha"}) == (
        "[ALPHA]"
    )


def test_read_only_execution_failure_can_deopt_to_fallback():
    fallback_calls = []

    def broken_read(key):
        raise RuntimeError(f"cannot read {key}")

    dispatcher = MetaToolDispatcher(
        {"read": broken_read, "format": lambda value: value},
        [_spec()],
        policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
        fallbacks={
            "read_then_format": lambda key: fallback_calls.append(key) or key,
        },
    )

    assert dispatcher.dispatch("read_then_format", {"key": "alpha"}) == "alpha"
    assert fallback_calls == ["alpha"]
    assert dispatcher.last_event.path == "fallback"
    assert "execution failed" in dispatcher.last_event.deopt_reason


def test_side_effecting_execution_failure_does_not_retry_fallback():
    fallback_calls = []

    def broken_read(key):
        raise RuntimeError(key)

    dispatcher = MetaToolDispatcher(
        {"read": broken_read, "format": lambda value: value},
        [_spec(side_effect_free=False)],
        policy=AccelerationPolicy(mode=DispatchMode.FORCE),
        fallbacks={
            "read_then_format": lambda key: fallback_calls.append(key) or key,
        },
    )

    with pytest.raises(RuntimeError, match="alpha"):
        dispatcher.dispatch("read_then_format", {"key": "alpha"})
    assert fallback_calls == []
    assert dispatcher.last_event.path == "meta"


def test_model_schema_exposure_is_explicit_and_policy_filtered():
    policy = AccelerationPolicy(
        mode=DispatchMode.SAFE_AUTO,
        expose_to_model=True,
    )
    dispatcher = MetaToolDispatcher(_primitives(), [_spec()], policy=policy)

    schemas = dispatcher.model_tool_schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "read_then_format"


def test_registry_rejects_duplicates():
    registry = MetaToolRegistry([_spec()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec())


def test_async_dispatch_and_async_fallback():
    async def read(key):
        return {"value": key.upper()}

    async def format(value):
        return f"[{value}]"

    safe = MetaToolDispatcher(
        {"read": read, "format": format},
        [_spec()],
        policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
    )
    assert asyncio.run(
        safe.adispatch("read_then_format", {"key": "alpha"})
    ) == "[ALPHA]"

    async def fallback(key):
        return f"baseline:{key}"

    disabled = MetaToolDispatcher(
        {"read": read, "format": format},
        [_spec()],
        policy=AccelerationPolicy(mode=DispatchMode.DISABLED),
        fallbacks={"read_then_format": fallback},
    )
    assert asyncio.run(
        disabled.adispatch("read_then_format", {"key": "alpha"})
    ) == "baseline:alpha"
