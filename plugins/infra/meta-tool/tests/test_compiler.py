import asyncio
import pytest
from pathlib import Path

from bate.compiler import CompilerOptions, MetaToolCompiler
from bate.candidates import MetaToolCandidate
from bate.graph import CallOccurrence
from bate.io import load_config, load_traces
from bate.models import ToolCall, Trace
from bate.normalization import NormalizationConfig, ToolRule, TraceNormalizer
from types import SimpleNamespace
from bate.runtime import MetaToolExecutor
from bate.spec import MetaToolSpec, StepSpec, input_binding, step_binding


def _compile_traces(traces, config=None, compiler_options=None):
    norm = TraceNormalizer(config)
    examples = tuple(tuple(CallOccurrence(t.trace_id, i, norm.canonicalize(c))
                           for i, c in enumerate(t.calls)) for t in traces)
    candidate = MetaToolCandidate(
        candidate_id="fixture", state_path=(),
        canonical_sequence=tuple(c.canonical.name for c in examples[0]),
        support=len(traces), occurrences=len(traces), purity=1.0,
        edge_weights=(), branch_dominance=(), incremental_decisions_saved=0,
        examples=examples,
    )
    return SimpleNamespace(specs=[MetaToolCompiler(compiler_options).compile(candidate)])


def _employee_result():
    traces = load_traces(Path(__file__).parent / "fixtures/employee_traces.jsonl")
    norm = TraceNormalizer(load_config(Path(__file__).parent / "fixtures/employee_config.json"))
    examples = tuple(tuple(CallOccurrence(t.trace_id, i, norm.canonicalize(c))
                           for i, c in enumerate(t.calls)) for t in traces)
    candidate = MetaToolCandidate(
        candidate_id="employee_workflow", state_path=(),
        canonical_sequence=tuple(c.canonical.name for c in examples[0]),
        support=3, occurrences=3, purity=1.0, edge_weights=(3, 3, 3),
        branch_dominance=(), incremental_decisions_saved=6,
        observed_llm_turn_occurrences=3, llm_turn_coverage=1.0,
        incremental_llm_calls_saved=6, mean_llm_calls_saved_per_observed_occurrence=2.0,
        examples=examples,
    )
    return SimpleNamespace(specs=[MetaToolCompiler().compile(candidate)])


def test_empty_container_type_changes_are_parameterized():
    traces = [Trace(f"t-{i}", (
        ToolCall("lookup", {}, {"id": f"item-{i}"}),
        ToolCall("read", {"id": f"item-{i}", "options": options}),
    )) for i, options in enumerate([[], {}, []])]
    spec = _compile_traces(traces).specs[0]
    assert spec.steps[1].arguments["options"] == {"$input": "options"}
    assert set(spec.input_schema["properties"]["options"]["type"]) == {"array", "object"}
    executor = MetaToolExecutor({"lookup": lambda: {"id": "live"},
                                 "read": lambda id, options: options})
    for value in [[], {}]:
        assert executor.execute(spec, {"options": value}).output == value


@pytest.mark.parametrize("options", [[], {}])
def test_empty_container_and_missing_field_are_inconsistent(options):
    traces = [Trace("present", (ToolCall("lookup", {}), ToolCall("read", {"options": options}))),
              Trace("absent", (ToolCall("lookup", {}), ToolCall("read", {})))]
    spec = _compile_traces(traces).specs[0]
    assert any("inconsistent argument shapes" in warning
               for warning in spec.metadata["compiler_warnings"])


def test_compiler_infers_inputs_and_result_dependencies():
    result = _employee_result()
    assert len(result.specs) == 1
    spec = result.specs[0]
    properties = spec.input_schema["properties"]
    assert set(properties) == {"name", "subject"}
    assert spec.steps[1].arguments["employee_id"] == {
        "$step": "step_1",
        "path": ["employee_id"],
    }
    assert spec.steps[2].arguments["attachment_id"] == {
        "$step": "step_2",
        "path": ["document_id"],
    }
    assert spec.metadata["llm_turn_coverage"] == 1.0
    assert (
        spec.metadata["mean_llm_calls_saved_per_observed_occurrence"] == 2.0
    )
    assert spec.metadata["side_effect_free"] is False
    assert spec.metadata["internal_data_flows"] == 2
    assert spec.metadata["varying_data_flows"] == 2
    assert spec.metadata["constant_data_flows"] == 0
    assert spec.metadata["data_flow_connected"] is True


def test_compiled_meta_tool_executes_deterministically():
    spec = _employee_result().specs[0]

    def search_employee(name):
        return {"employee_id": f"id:{name}"}

    def get_document(employee_id, kind):
        assert kind == "passport"
        return {"document_id": f"doc:{employee_id}"}

    def send_email(to, subject, attachment_id):
        return {
            "to": to,
            "subject": subject,
            "attachment_id": attachment_id,
        }

    executor = MetaToolExecutor(
        {
            "directory.search_employee": search_employee,
            "documents.get_document": get_document,
            "mail.send_email": send_email,
        }
    )
    result = executor.execute(
        spec, {"name": "Dana", "subject": "Dana passport"}
    )
    assert result.output == {
        "to": "hr@example.test",
        "subject": "Dana passport",
        "attachment_id": "doc:id:Dana",
    }
    assert len(result.steps) == 3


def test_meta_tool_schema_is_provider_neutral_function_schema():
    spec = _employee_result().specs[0]
    schema = spec.as_tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == spec.name


def test_compiled_meta_tool_supports_async_primitive_tools():
    spec = _employee_result().specs[0]

    async def search_employee(name):
        return {"employee_id": f"id:{name}"}

    async def get_document(employee_id, kind):
        return {"document_id": f"doc:{employee_id}:{kind}"}

    async def send_email(to, subject, attachment_id):
        return {"to": to, "subject": subject, "attachment_id": attachment_id}

    executor = MetaToolExecutor(
        {
            "directory.search_employee": search_employee,
            "documents.get_document": get_document,
            "mail.send_email": send_email,
        }
    )
    result = asyncio.run(
        executor.aexecute(spec, {"name": "Eve", "subject": "Eve passport"})
    )
    assert result.output["attachment_id"] == "doc:id:Eve:passport"


def test_step_binding_falls_back_when_optional_result_list_is_empty():
    spec = MetaToolSpec(
        name="wait_then_continue",
        description="Continue a pool after waiting for an event.",
        input_schema={
            "type": "object",
            "properties": {"pool_id": {"type": "string"}},
            "required": ["pool_id"],
            "additionalProperties": False,
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


def test_compiler_emits_input_fallback_for_ambiguous_output_dependency():
    examples = []
    normalizer = TraceNormalizer()
    for index in range(3):
        pool_id = f"pool-{index}"
        calls = (
            ToolCall(
                "wait",
                {"pool_id": pool_id},
                {"events": [{"pool_id": pool_id}]},
            ),
            ToolCall(
                "continue_pool",
                {"pool_id": pool_id},
                {"pool_id": pool_id},
            ),
            ToolCall(
                "wait",
                {"pool_id": pool_id},
                {"events": [{"pool_id": pool_id, "kind": "ready"}]},
            ),
        )
        examples.append(
            tuple(
                CallOccurrence(
                    trace_id=f"trace-{index}",
                    call_index=call_index,
                    canonical=normalizer.canonicalize(call),
                )
                for call_index, call in enumerate(calls)
            )
        )
    spec = MetaToolCompiler().compile(
        MetaToolCandidate(
            candidate_id="wait_then_continue",
            state_path=("s0", "s1", "s2", "s3"),
            canonical_sequence=("wait", "continue_pool", "wait"),
            support=3,
            occurrences=3,
            purity=1.0,
            edge_weights=(3, 3, 3),
            branch_dominance=(1.0, 1.0, 1.0),
            incremental_decisions_saved=3,
            examples=tuple(examples),
        )
    )

    assert spec.steps[1].arguments["pool_id"] == {
        "$step": "step_1",
        "path": ["events", 0, "pool_id"],
        "fallback": {"$input": "pool_id"},
    }
    assert spec.steps[2].arguments["pool_id"] == {
        "$step": "step_2",
        "path": ["pool_id"],
        "fallback": {"$input": "pool_id"},
    }
    assert spec.metadata["fallback_step_binding_count"] == 2

    calls = []

    async def wait(pool_id):
        calls.append(("wait", pool_id))
        return {"pool_id": pool_id, "events": []}

    async def continue_pool(pool_id):
        calls.append(("continue", pool_id))
        return {"pool_id": pool_id}

    executor = MetaToolExecutor(
        {
            "wait": wait,
            "continue_pool": continue_pool,
        }
    )
    output = asyncio.run(executor.aexecute(spec, {"pool_id": "pool-new"}))
    assert calls == [
        ("wait", "pool-new"),
        ("continue", "pool-new"),
        ("wait", "pool-new"),
    ]
    assert output.output == {"pool_id": "pool-new", "events": []}


def test_compiler_warns_about_unbound_runtime_local_element_refs():
    traces = [
        Trace(
            trace_id=trace_id,
            success=True,
            calls=(
                ToolCall("click", {"ref": "e8"}, turn_id=1),
                ToolCall("fill", {"ref": "e10", "text": "hello"}, turn_id=2),
            ),
        )
        for trace_id in ("a", "b")
    ]
    result = _compile_traces(
        traces,
        config=NormalizationConfig(
            tool_rules=(
                ToolRule(pattern="^(click|fill)$", ignore_arguments=("ref",)),
            )
        ),
    )

    warnings = result.specs[0].metadata["compiler_warnings"]
    assert any("argument ref looks runtime-local" in warning for warning in warnings)
    assert any("replay may target stale state" in warning for warning in warnings)


def test_compiler_can_bind_verified_stable_runtime_local_dependencies():
    traces = [
        Trace(
            trace_id=trace_id,
            success=True,
            calls=(
                ToolCall(
                    "observe",
                    result={"elements": [{"ref": "e10"}]},
                    turn_id=1,
                ),
                ToolCall("click", {"ref": "e10"}, turn_id=2),
            ),
        )
        for trace_id in ("a", "b")
    ]
    result = _compile_traces(
        traces,
        config=NormalizationConfig(
            tool_rules=(
                ToolRule(pattern="^(observe|click)$", ignore_arguments=("ref",)),
            )
        ),
        compiler_options=CompilerOptions(
            bind_stable_runtime_dependencies=True,
        ),
    )

    assert len(result.specs) == 1
    spec = result.specs[0]
    assert spec.steps[1].arguments["ref"] == {
        "$step": "step_1",
        "path": ["elements", 0, "ref"],
    }
    assert spec.metadata["varying_data_flows"] == 0
    assert spec.metadata["stable_runtime_data_flows"] == 1
    assert spec.metadata["internal_data_flows"] == 1
    assert "compiler_warnings" not in spec.metadata


def test_compiler_can_parameterize_unbound_runtime_local_entry_arguments():
    traces = [
        Trace(
            trace_id=trace_id,
            success=True,
            calls=(
                ToolCall(
                    "click",
                    {"ref": "e10"},
                    {"ref": "e11"},
                    turn_id=1,
                ),
                ToolCall("fill", {"ref": "e11", "text": "hello"}, turn_id=2),
            ),
        )
        for trace_id in ("a", "b")
    ]
    result = _compile_traces(
        traces,
        config=NormalizationConfig(
            tool_rules=(
                ToolRule(pattern="^(click|fill)$", ignore_arguments=("ref",)),
            )
        ),
        compiler_options=CompilerOptions(
            parameterize_unbound_runtime_arguments=True,
            bind_stable_runtime_dependencies=True,
        ),
    )

    assert len(result.specs) == 1
    spec = result.specs[0]
    assert spec.steps[0].arguments["ref"] == {"$input": "ref"}
    assert spec.steps[1].arguments["ref"] == {
        "$step": "step_1",
        "path": ["ref"],
    }
    assert "compiler_warnings" not in spec.metadata
    assert spec.metadata["runtime_local_input_count"] == 1


def test_compiler_does_not_bind_fields_that_only_share_a_constant_value():
    traces = []
    for index in range(2):
        run_id = f"run-{index}"
        traces.append(
            Trace(
                trace_id=f"trace-{index}",
                success=True,
                calls=(
                    ToolCall(
                        "create",
                        result={"run_id": run_id, "goal_revision": 1},
                    ),
                    ToolCall(
                        "plan",
                        {"run_id": run_id, "requested_k": 1},
                    ),
                ),
            )
        )
    result = _compile_traces(
        traces,
        config=NormalizationConfig(
            tool_rules=(
                ToolRule(
                    pattern="^plan$",
                    placeholder_arguments={"run_id": "{RUN_ID}"},
                ),
            )
        ),
    )

    assert result.specs[0].steps[1].arguments["run_id"] == {
        "$step": "step_1",
        "path": ["run_id"],
    }
    assert result.specs[0].steps[1].arguments["requested_k"] == 1
    assert result.specs[0].metadata["internal_data_flows"] == 1
    assert result.specs[0].metadata["observed_internal_data_flows"] == 2
    assert result.specs[0].metadata["constant_data_flows"] == 1
