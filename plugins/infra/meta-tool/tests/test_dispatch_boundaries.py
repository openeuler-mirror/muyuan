"""Policy/fallback must not bypass the executor's input validation."""
import asyncio
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from bate.dispatch import AccelerationPolicy, DispatchMode, MetaToolDispatcher
from bate.runtime import MetaToolValidationError
from bate.spec import MetaToolSpec, StepSpec, input_binding, step_binding


def _spec():
    return MetaToolSpec(
        name="validate_then_read", description="Dispatch validation fixture",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                      "required": ["value"], "additionalProperties": False},
        steps=(StepSpec("first", "first", {"value": input_binding("value")}),
               StepSpec("second", "second", {"value": step_binding("first")})),
        output=step_binding("second"),
        metadata={"reviewed": True, "side_effect_free": True,
                  "internal_data_flows": 1, "varying_data_flows": 1,
                  "sequence_purity": 1, "min_branch_dominance": 1,
                  "llm_turn_coverage": 1,
                  "mean_llm_calls_saved_per_observed_occurrence": 1},
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mode", list(DispatchMode))
@pytest.mark.parametrize("inputs", [{"value": 123}, {"value": "valid", "extra": 1}])
def test_invalid_inputs_never_execute_primitives_or_fallback(asynchronous, mode, inputs):
    calls = []

    def invoked(**kwargs):
        calls.append(kwargs)
        return kwargs["value"]

    spec = _spec()
    dispatcher = MetaToolDispatcher(
        {"first": invoked, "second": invoked}, [spec],
        policy=AccelerationPolicy(mode=mode), fallbacks={spec.name: invoked},
    )
    with pytest.raises(MetaToolValidationError):
        if asynchronous:
            asyncio.run(dispatcher.adispatch(spec.name, inputs))
        else:
            dispatcher.dispatch(spec.name, inputs)
    assert calls == []


def test_missing_validation_dependency_never_falls_back(monkeypatch):
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
    spec = _spec()
    dispatcher = MetaToolDispatcher(
        {}, [spec], policy=AccelerationPolicy(mode=DispatchMode.FORCE),
        fallbacks={spec.name: lambda **kw: calls.append(kw)},
    )
    with pytest.raises(MetaToolValidationError, match="requires jsonschema"):
        dispatcher.dispatch(spec.name, {"value": "valid"})
    assert not calls


def test_cancellation_propagates_without_fallback():
    calls = []

    async def cancelled(**kwargs):
        raise asyncio.CancelledError()

    spec = _spec()
    dispatcher = MetaToolDispatcher(
        {"first": cancelled, "second": cancelled}, [spec],
        policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
        fallbacks={spec.name: lambda **kw: calls.append(kw)},
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dispatcher.adispatch(spec.name, {"value": "valid"}))
    assert calls == []


def test_telemetry_failure_does_not_repeat_execution():
    calls = []

    def invoked(value):
        calls.append(value)
        return value

    def broken_telemetry(event):
        raise RuntimeError("telemetry unavailable")

    spec = _spec()
    dispatcher = MetaToolDispatcher(
        {"first": invoked, "second": invoked}, [spec],
        policy=AccelerationPolicy(mode=DispatchMode.SAFE_AUTO),
        telemetry=broken_telemetry,
    )
    assert dispatcher.dispatch(spec.name, {"value": "valid"}) == "valid"
    assert calls == ["valid", "valid"]
    assert dispatcher.telemetry_failures == 1


def test_dispatch_imports_without_unavailable_algorithm_layers(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent("""
        import importlib.abc
        from pathlib import Path
        import sys
        source = Path(sys.argv[1])
        future = tuple('bate.' + name for name in (
            'normalization', 'graph', 'compiler', 'dataflow', 'extraction', 'pipeline', 'cli'
        ) if not (source / 'bate' / (name + '.py')).exists())
        class BlockFutureLayers(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, *args):
                blocked = ('awo', 'jsonschema', 'referencing') + future
                if any(fullname == x or fullname.startswith(x + '.') for x in blocked):
                    raise ModuleNotFoundError(fullname)
        sys.meta_path.insert(0, BlockFutureLayers())
        sys.path.insert(0, sys.argv[1])
        from bate.dispatch import AccelerationPolicy, DispatchMode, MetaToolDispatcher
        from bate.admission import auto_admit_specs
        from bate.read_batches import read_batch_layout
        assert AccelerationPolicy().mode is DispatchMode.DISABLED
        assert len(read_batch_layout(2)[1]) == 2
        assert auto_admit_specs([]) == ([], [])
        assert 'jsonschema' not in sys.modules
    """)
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(root / "src")],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_dispatch_documentation_examples_execute():
    root = Path(__file__).resolve().parents[1]
    source = (root / "docs/safe-dispatch.md").read_text()
    snippets = [block.split("```", 1)[0] for block in source.split("```python\n")[1:]]
    assert snippets
    for snippet in snippets:
        exec(compile(snippet, "docs/safe-dispatch.md", "exec"), {})
