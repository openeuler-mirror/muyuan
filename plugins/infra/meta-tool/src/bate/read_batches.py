"""Opt-in caller-planned read compositions; never infer the next code to inspect.

Unlike output-dependent workflows, every primitive argument is supplied before
execution. Historical cross-turn occurrences establish reuse, not proof that a
particular future model decision can be removed. Default BATE admission is unchanged.
"""
from __future__ import annotations

from collections import defaultdict
from math import isfinite
from typing import Iterable

from .models import Trace
from .spec import MetaToolSpec, StepSpec, input_binding, step_binding

KIND = "caller_bound_read_batch_v1"
MAX_READ_LINES = 300
MAX_PATH_LENGTH = 4096


def read_batch_layout(size: int) -> tuple[dict, tuple[StepSpec, ...], list]:
    if size not in (2, 3, 4):
        raise ValueError("read batch size must be 2, 3 or 4")
    properties, steps, output = {}, [], []
    for index in range(1, size + 1):
        for name, schema in (
            ("path", {"type": "string", "minLength": 1, "maxLength": MAX_PATH_LENGTH}),
            ("offset", {"type": "integer", "minimum": 1}),
            ("limit", {"type": "integer", "minimum": 1, "maximum": MAX_READ_LINES}),
        ):
            properties[f"{name}_{index}"] = schema
        arguments = {name: input_binding(f"{name}_{index}") for name in ("path", "offset", "limit")}
        step_id = f"read_{index}"
        steps.append(StepSpec(step_id, "read", arguments))
        output.append({"path": input_binding(f"path_{index}"),
                       "offset": input_binding(f"offset_{index}"),
                       "limit": input_binding(f"limit_{index}"),
                       "result": step_binding(step_id)})
    return ({"type": "object", "properties": properties,
             "required": list(properties), "additionalProperties": False}, tuple(steps), output)


def is_caller_bound_read_batch(spec: MetaToolSpec) -> bool:
    """Validate the complete bounded shape, not merely a self-declared flag."""
    if spec.metadata.get("composition_kind") != KIND or len(spec.steps) not in (2, 3, 4):
        return False
    schema, steps, output = read_batch_layout(len(spec.steps))
    if spec.input_schema != schema or spec.steps != steps or spec.output != output:
        return False
    for key, minimum in (("support", 3), ("distinct_sessions", 2),
                         ("llm_turn_coverage", 1),
                         ("mean_llm_calls_saved_per_observed_occurrence", 1)):
        value = spec.metadata.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or value < minimum):
            return False
    return (spec.metadata.get("side_effect_free") is True
            and spec.metadata.get("execution_guard") == "none"
            and spec.metadata.get("requires_caller_plan") is True)


def _eligible(call) -> bool:
    a = call.arguments
    return (call.name == "read" and not call.metadata.get("is_error")
            and set(a) == {"path", "offset", "limit"}
            and isinstance(a["path"], str) and 0 < len(a["path"]) <= MAX_PATH_LENGTH
            and type(a["offset"]) is int and a["offset"] >= 1
            and type(a["limit"]) is int and 1 <= a["limit"] <= MAX_READ_LINES
            and call.turn_id is not None)


def extract_read_batches(traces: Iterable[Trace]) -> tuple[list[MetaToolSpec], dict]:
    """Extract only parameter-free patterns; never retain paths or code in specs.

    Counts include overlapping occurrences. Same-turn parallel reads contribute
    no savings evidence and are excluded. The caller must still plan all ranges.
    """
    evidence = defaultdict(list)
    for trace in traces:
        if not trace.success:
            continue
        session = trace.metadata.get("session_id")
        if not isinstance(session, str) or not session:
            continue
        for size in (2, 3, 4):
            for start in range(len(trace.calls) - size + 1):
                calls = trace.calls[start:start + size]
                if not all(_eligible(call) for call in calls):
                    continue
                turns = len({call.turn_id for call in calls})
                if turns < 2:
                    continue
                evidence[size].append((trace.trace_id, session, turns - 1))
    specs, report = [], {"composition_kind": KIND, "candidates": []}
    for size in (2, 3, 4):
        examples = evidence[size]
        support = len({x[0] for x in examples})
        sessions = len({x[1] for x in examples})
        item = {"steps": size, "support": support, "distinct_sessions": sessions,
                "occurrences": len(examples), "admitted": support >= 3 and sessions >= 2,
                "potential_turns_saved_sum": sum(x[2] for x in examples),
                "savings_are_conditional_and_overlapping": True}
        report["candidates"].append(item)
        if not item["admitted"]:
            continue
        schema, steps, output = read_batch_layout(size)
        specs.append(MetaToolSpec(
            name=f"meta_tool_read_batch_{size}",
            description=f"Read {size} caller-planned code ranges, returning every result. All ranges must be known before execution; no semantic planning inside.",
            input_schema=schema, steps=steps, output=output,
            metadata={"composition_kind": KIND, "support": support,
                "distinct_sessions": sessions, "occurrences": len(examples),
                "requires_caller_plan": True, "side_effect_free": True,
                "execution_guard": "none", "reviewed": True,
                "human_reviewed": False, "requires_domain_review": False,
                "admission_policy": "bounded-caller-read-profile-v1",
                "internal_data_flows": 0, "varying_data_flows": 0,
                "sequence_purity": 1.0, "min_branch_dominance": 1.0,
                "llm_turn_coverage": 1.0,
                "mean_llm_calls_saved_per_observed_occurrence": sum(x[2] for x in examples) / len(examples),
                "savings_semantics": "conditional upper bound for already planned ranges, not measured online savings",
                "intent": "read source code ranges inspect files", "project_tags": []},
        ))
    return specs, report
