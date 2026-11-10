"""Compare the Pi fast path with Python using identical specs and policies."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from bate.dispatch import AccelerationPolicy, DispatchMode
from bate.spec import MetaToolSpec
from bate.validation import MetaToolValidationError, validate_inputs

PACKAGE = Path(__file__).resolve().parents[1] / "integrations/pi-meta-tool"


def _run_node(rows):
    node = shutil.which("node")
    if not node or not (PACKAGE / "node_modules/ajv").is_dir():
        pytest.skip("Pi parity checks require Node.js 22.18+ and npm ci")
    script = """
import { readFileSync } from 'node:fs';
const { directExecutionAllowed, executeDirectMetaTool } = await import(process.argv[1]);
const rows = JSON.parse(readFileSync(0, 'utf8'));
const results = [];
for (const row of rows) {
  const allowed = directExecutionAllowed(row.spec, row.mode, row.policy);
  let calls = 0, valid = true;
  if (row.inputs) {
    try { await executeDirectMetaTool(row.spec, row.inputs, async () => { calls++; return {id: 'live'}; }); }
    catch { valid = false; }
  }
  results.push({allowed, valid, calls});
}
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script,
         (PACKAGE / "src/direct-execution.ts").as_uri()],
        input=json.dumps(rows), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _spec():
    return {
        "name": "fixture", "description": "read a resolved record",
        "input_schema": {"type": "object", "properties": {"value": {}},
                         "required": ["value"], "additionalProperties": False},
        "steps": [
            {"id": "lookup", "tool": "lookup", "arguments": {"value": {"$input": "value"}}},
            {"id": "read", "tool": "read", "arguments": {"id": {"$step": "lookup", "path": ["id"]}}},
        ], "output": {"$step": "read"},
        "metadata": {"execution_guard": "none", "side_effect_free": True, "reviewed": True,
                     "internal_data_flows": 1, "varying_data_flows": 1,
                     "sequence_purity": 1, "min_branch_dominance": 1, "llm_turn_coverage": 1,
                     "mean_llm_calls_saved_per_observed_occurrence": 1},
    }


def test_fast_policy_matches_python_for_flow_thresholds_and_review_exemptions():
    cases = [
        ({}, {}), ({}, {"min_varying_data_flows": 2}),
        ({"internal_data_flows": 2}, {}),
        ({"varying_data_flows": 0}, {}),
        ({"varying_data_flows": "1"}, {}),
        ({"varying_data_flows": True}, {}),
        ({"varying_data_flows": None}, {"min_varying_data_flows": 0}),
        ({"data_flow_reviewed": True, "varying_data_flows": 0}, {"min_varying_data_flows": 2}),
        ({"stable_runtime_data_flows": 1, "runtime_local_data_flow_verified": True,
          "varying_data_flows": 0}, {"allow_stable_runtime_data_flows": True}),
        ({"stable_runtime_data_flows": 1, "runtime_local_data_flow_verified": False,
          "varying_data_flows": 0}, {"allow_stable_runtime_data_flows": True}),
        ({"llm_turn_coverage": "1"}, {}),
        ({"reviewed": False}, {}),
        ({"reviewed": False}, {"require_reviewed": False}),
        ({"compiler_warnings": ["unsafe"]}, {}),
    ]
    rows, expected = [], []
    for metadata, policy in cases:
        spec = _spec()
        spec["metadata"].update(metadata)
        for mode in ("safe_auto", "force"):
            rows.append({"spec": spec, "mode": mode, "policy": policy})
            expected.append(AccelerationPolicy(mode=DispatchMode(mode), **policy)
                            .evaluate(MetaToolSpec.from_dict(spec)).allowed)
    for row, actual, allowed in zip(rows, _run_node(rows), expected):
        assert actual["allowed"] == allowed, row


def test_fast_schema_validation_matches_python_before_any_primitive():
    cases = [
        ({"type": "integer", "minimum": 1, "maximum": 300}, [1, 300, 301, "1", True, 1.5]),
        ({"type": "string", "minLength": 1, "maxLength": 2}, ["", "abc", "😀", "😀😀"]),
        ({"enum": [1, False, {"x": []}]}, [1, True, False, {"x": []}, {"x": {}}]),
        ({"type": "array", "items": {"type": "integer"}, "minItems": 1,
          "maxItems": 2, "uniqueItems": True}, [[], [1], [1, 1], [1, 2, 3], ["1"]]),
        ({"type": "object", "properties": {"kind": {"enum": ["brief", "full"]}},
          "required": ["kind"], "additionalProperties": False},
         [{}, {"kind": "brief"}, {"kind": "other"}, {"kind": "full", "extra": 1}]),
        ({"anyOf": [{"type": "null"}, {"type": "integer"}]}, [None, 2, "2"]),
        ({"$defs": {"limit": {"type": "integer", "maximum": 3}}, "$ref": "#/$defs/limit"},
         [2, 4, "2"]),
        ({"type": "string", "format": "email"}, ["annotation only"]),
    ]
    rows, expected = [], []
    for schema, values in cases:
        spec = _spec()
        if "$defs" in schema:
            schema = deepcopy(schema)
            spec["input_schema"]["$defs"] = schema.pop("$defs")
        spec["input_schema"]["properties"]["value"] = schema
        for value in values:
            inputs = {"value": value}
            try:
                validate_inputs(spec["input_schema"], inputs)
                valid = True
            except MetaToolValidationError:
                valid = False
            rows.append({"spec": spec, "mode": "safe_auto", "policy": {}, "inputs": inputs})
            expected.append(valid)
    for row, actual, valid in zip(rows, _run_node(rows), expected):
        assert actual == {"allowed": True, "valid": valid, "calls": 2 if valid else 0}, row
