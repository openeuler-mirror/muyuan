#!/usr/bin/env python3
"""Check task-local semantic requirements that ranking intentionally omits."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[4]
SCORING = WORKSPACE / "scoring.py"


def _load_scoring():
    spec = importlib.util.spec_from_file_location("guarded_scoring", SCORING)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCORING}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expected(values: list[float]) -> list[float]:
    return [float(value) * float(value) + 1.0 for value in values]


def check() -> dict[str, object]:
    module = _load_scoring()
    values = [1.0, 2.0, -3.0]
    first = module.score_batch(values)
    values[0] = 4.0
    second = module.score_batch(values)
    equivalent = module.score_batch(list(values))

    tree = ast.parse(SCORING.read_text(encoding="utf-8"), filename=str(SCORING))
    uses_object_identity = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "id"
        for node in ast.walk(tree)
    )
    violations: list[str] = []
    if first != _expected([1.0, 2.0, -3.0]):
        violations.append("initial_result_mismatch")
    if second != _expected(values):
        violations.append("stale_result_after_input_mutation")
    if equivalent != second:
        violations.append("equivalent_inputs_depend_on_identity")
    if uses_object_identity:
        violations.append("input_object_identity_shortcut")

    return {
        "valid": not violations,
        "violations": violations,
        "first": first,
        "after_mutation": second,
        "expected_after_mutation": _expected(values),
    }


if __name__ == "__main__":
    result = check()
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["valid"] else 1)
