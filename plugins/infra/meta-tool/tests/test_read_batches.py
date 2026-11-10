import copy
import json

import pytest

from bate.dispatch import AccelerationPolicy, DispatchMode
from bate.models import Trace, ToolCall
from bate.read_batches import extract_read_batches, is_caller_bound_read_batch
from bate.runtime import MetaToolExecutor
from bate.spec import MetaToolSpec


def traces(same_turn=False):
    return [Trace(f"trace-{i}", tuple(ToolCall("read", {
        "path": "/workspace/" + "long-directory/" * 8 + f"source-{i}.c",
        "offset": j * 20 + 1, "limit": 20}, result=f"code-{i}-{j}",
        turn_id=1 if same_turn else j) for j in range(4)),
        success=True, metadata={"session_id": f"session-{i}"}) for i in range(3)]


def test_explicit_profile_required_and_no_training_values_in_catalog():
    specs, report = extract_read_batches(traces())
    assert [len(s.steps) for s in specs] == [2, 3, 4]
    assert report["candidates"][0]["occurrences"] == 9
    for spec in specs:
        assert is_caller_bound_read_batch(spec)
        assert not AccelerationPolicy(mode=DispatchMode.SAFE_AUTO).evaluate(spec).allowed
        assert AccelerationPolicy(mode=DispatchMode.SAFE_AUTO,
            allow_caller_bound_read_batches=True).evaluate(spec).allowed
        assert "long-directory" not in json.dumps(spec.to_dict())
        assert "code-" not in json.dumps(spec.to_dict())


def test_same_turn_reads_and_missing_independent_support_are_not_savings():
    assert extract_read_batches(traces(same_turn=True))[0] == []
    assert extract_read_batches(traces()[:2])[0] == []


def test_read_batch_returns_every_output_and_validates_before_side_effects():
    spec = extract_read_batches(traces())[0][0]
    calls = []
    def read(**args):
        calls.append(args)
        return f"EXACT {args['path']}:{args['offset']}\ntrailing newline\n"
    executor = MetaToolExecutor({"read": read})
    inputs = {"path_1": "a.c", "offset_1": 10, "limit_1": 20,
              "path_2": "b.c", "offset_2": 50, "limit_2": 30}
    out = executor.execute(spec, inputs)
    assert out.output == [
        {"path": "a.c", "offset": 10, "limit": 20, "result": "EXACT a.c:10\ntrailing newline\n"},
        {"path": "b.c", "offset": 50, "limit": 30, "result": "EXACT b.c:50\ntrailing newline\n"},
    ]
    calls.clear()
    with pytest.raises(ValueError):
        executor.execute(spec, {**inputs, "limit_2": 301})
    assert calls == []


@pytest.mark.parametrize("mutate", [
    lambda s: s["steps"][0].update(tool="bash"),
    lambda s: s.update(output={"$step": "read_2"}),
    lambda s: s["input_schema"]["properties"]["limit_1"].update(maximum=100000),
    lambda s: s["steps"][0]["arguments"].update(path="frozen-secret-path"),
    lambda s: s["metadata"].update(distinct_sessions=1),
    lambda s: s["metadata"].update(support=float("inf")),
])
def test_shape_or_evidence_cannot_be_bypassed_by_metadata(mutate):
    raw = copy.deepcopy(extract_read_batches(traces())[0][0].to_dict())
    mutate(raw)
    spec = MetaToolSpec.from_dict(raw)
    assert not is_caller_bound_read_batch(spec)
    assert not AccelerationPolicy(mode=DispatchMode.SAFE_AUTO,
        allow_caller_bound_read_batches=True).evaluate(spec).allowed
