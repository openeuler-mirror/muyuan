from dataclasses import replace

import pytest

from bate.admission import auto_admit_specs
from bate.spec import MetaToolSpec, StepSpec, input_binding, step_binding


def read_spec(**metadata):
    return MetaToolSpec(
        name="lookup_and_read",
        description="Look up an identifier and read its record.",
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        steps=(
            StepSpec("lookup", "lookup", {"key": input_binding("key")}),
            StepSpec("read", "read", {"id": step_binding("lookup", ("id",))}),
        ),
        output=step_binding("read"),
        metadata={
            "distinct_sessions": 3,
            "side_effect_free": True,
            "internal_data_flows": 1,
            "varying_data_flows": 1,
            "sequence_purity": 1.0,
            "min_branch_dominance": 1.0,
            "llm_turn_coverage": 1.0,
            "mean_llm_calls_saved_per_observed_occurrence": 1.0,
            **metadata,
        },
    )


POLICY = {
    "read_only_tools": ["lookup", "read"],
    "min_distinct_sessions": 3,
    "allow_confirmed_runtime_derived_mutations": False,
}


def test_local_admission_preserves_executable_contract():
    original = read_spec()
    active, rejected = auto_admit_specs(
        [original], POLICY, adapter_supported_tools={"lookup", "read"}
    )
    assert not rejected
    assert len(active) == 1
    assert active[0].steps == original.steps
    assert active[0].input_schema == original.input_schema
    assert active[0].output == original.output
    assert active[0].metadata["execution_guard"] == "none"
    assert active[0].metadata["auto_approved"] is True
    assert "auto_approved" not in original.metadata


@pytest.mark.parametrize(
    "metadata,reason",
    [
        ({"distinct_sessions": 2}, "insufficient distinct session"),
        ({"tool_schema_conflicts": {"read": ["a", "b"]}}, "inconsistent primitive schemas"),
        ({"compiler_warnings": ["inconsistent argument shapes"]}, "compiler"),
        ({"internal_data_flows": 0, "varying_data_flows": 0}, "internal_data_flows"),
    ],
)
def test_local_admission_keeps_evidence_gates(metadata, reason):
    active, rejected = auto_admit_specs(
        [read_spec(**metadata)], POLICY, adapter_supported_tools={"lookup", "read"}
    )
    assert not active
    assert reason in " ".join(rejected[0]["reasons"])


def test_local_admission_rejects_unavailable_tools():
    active, rejected = auto_admit_specs(
        [read_spec()], POLICY, adapter_supported_tools={"lookup"}
    )
    assert not active
    assert any("unavailable primitive tools" in reason for reason in rejected[0]["reasons"])


def test_local_admission_keeps_runtime_derived_mutation_guard():
    spec = read_spec(side_effect_free=False)
    spec = replace(spec, steps=(
        spec.steps[0],
        StepSpec("write", "write", {"value": step_binding("lookup", ("id",))}),
    ), output=step_binding("write"))
    active, rejected = auto_admit_specs(
        [spec], POLICY, adapter_supported_tools={"lookup", "write"}
    )
    assert not active
    assert any("runtime-derived arguments" in reason for reason in rejected[0]["reasons"])
