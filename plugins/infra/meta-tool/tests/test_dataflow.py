from bate.dataflow import analyze_data_flow
from bate.graph import CallOccurrence
from bate.models import ToolCall
from bate.normalization import TraceNormalizer


def _examples(raw_examples):
    normalizer = TraceNormalizer()
    return tuple(
        tuple(
            CallOccurrence(
                trace_id=f"trace-{trace_index}",
                call_index=call_index,
                canonical=normalizer.canonicalize(call),
            )
            for call_index, call in enumerate(calls)
        )
        for trace_index, calls in enumerate(raw_examples)
    )


def test_detects_only_dependencies_verified_across_every_occurrence():
    analysis = analyze_data_flow(
        _examples(
            [
                [
                    ToolCall("lookup", {"query": "fixed"}, {"id": "A"}),
                    ToolCall("read", {"id": "A"}),
                ],
                [
                    ToolCall("lookup", {"query": "fixed"}, {"id": "B"}),
                    ToolCall("read", {"id": "B"}),
                ],
            ]
        )
    )

    spans = analysis.connected_spans()
    assert len(spans) == 1
    assert spans[0].internal_flows == 1
    assert spans[0].varying_flows == 1
    assert spans[0].constant_flows == 0


def test_empty_containers_are_not_silently_dropped_from_evidence():
    analysis = analyze_data_flow(_examples([
        [ToolCall("read", {"options": []})],
        [ToolCall("read", {"options": {}})],
    ]))
    evidence = analysis.argument(0, ("options",))
    assert evidence is not None and evidence.kind == "free_text"
    missing = analyze_data_flow(_examples([
        [ToolCall("read", {"options": []})], [ToolCall("read", {})],
    ]))
    assert ("options",) in missing.steps[0].inconsistent_paths


def test_constant_equality_is_not_counted_as_varying_flow():
    analysis = analyze_data_flow(
        _examples(
            [
                [
                    ToolCall("lookup", {}, {"tier": "gold"}),
                    ToolCall("read", {"tier": "gold"}),
                ],
                [
                    ToolCall("lookup", {}, {"tier": "gold"}),
                    ToolCall("read", {"tier": "gold"}),
                ],
            ]
        )
    )

    span = analysis.connected_spans()[0]
    assert span.internal_flows == 1
    assert span.varying_flows == 0
    assert span.constant_flows == 1
    assert span.runtime_local_flows == 0


def test_constant_runtime_local_dependency_is_classified_separately():
    analysis = analyze_data_flow(
        _examples(
            [
                [
                    ToolCall("observe", {}, {"ref": "e10"}),
                    ToolCall("click", {"ref": "e10"}),
                ],
                [
                    ToolCall("observe", {}, {"ref": "e10"}),
                    ToolCall("click", {"ref": "e10"}),
                ],
            ]
        )
    )

    span = analysis.connected_spans()[0]
    assert span.internal_flows == 1
    assert span.varying_flows == 0
    assert span.constant_flows == 1
    assert span.runtime_local_flows == 1


def test_independent_free_text_commands_do_not_form_a_meta_tool_span():
    analysis = analyze_data_flow(
        _examples(
            [
                [
                    ToolCall("code.execute", {"command": "python -c 'print(1)'"}),
                    ToolCall("code.execute", {"command": "pytest tests/test_a.py"}),
                ],
                [
                    ToolCall("code.execute", {"command": "python -c 'print(2)'"}),
                    ToolCall("code.execute", {"command": "pytest tests/test_b.py"}),
                ],
            ]
        )
    )

    assert analysis.connected_spans() == ()
    assert all(
        step.arguments[0].kind == "free_text" for step in analysis.steps
    )


def test_bridge_allows_parameters_but_stops_at_free_text_glue():
    bounded = analyze_data_flow(
        _examples(
            [
                [
                    ToolCall("lookup", {}, {"id": "A"}),
                    ToolCall("load", {"id": "A"}, {"token": "TA"}),
                    ToolCall("audit", {"code": "x1"}),
                    ToolCall("render", {"token": "TA"}),
                ],
                [
                    ToolCall("lookup", {}, {"id": "B"}),
                    ToolCall("load", {"id": "B"}, {"token": "TB"}),
                    ToolCall("audit", {"code": "x2"}),
                    ToolCall("render", {"token": "TB"}),
                ],
            ]
        )
    )
    free_text = analyze_data_flow(
        _examples(
            [
                [
                    ToolCall("lookup", {}, {"id": "A"}),
                    ToolCall("load", {"id": "A"}, {"token": "TA"}),
                    ToolCall("agent.glue", {"text": "write arbitrary source one"}),
                    ToolCall("render", {"token": "TA"}),
                ],
                [
                    ToolCall("lookup", {}, {"id": "B"}),
                    ToolCall("load", {"id": "B"}, {"token": "TB"}),
                    ToolCall("agent.glue", {"text": "write arbitrary source two"}),
                    ToolCall("render", {"token": "TB"}),
                ],
            ]
        )
    )

    assert bounded.connected_spans()[0].n_steps == 4
    assert free_text.connected_spans()[0].n_steps == 2
