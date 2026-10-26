#!/usr/bin/env python3
"""Static Triton-Ascend shape verifier for the explicitly non-NPU smoke path."""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--function", required=True)
    args = parser.parse_args()
    package = Path("cann_bench")
    errors: list[str] = []
    sources: list[str] = []
    found_function = False
    for path in sorted(package.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        sources.append(source)
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path}: {exc}")
            continue
        found_function = found_function or any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == args.function
            for node in ast.walk(tree)
        )
    combined = "\n".join(sources)
    if not found_function:
        errors.append(f"missing function definition: {args.function}")
    if "triton" not in combined:
        errors.append("submission has no Triton import or reference")
    scores = [float(item) for item in re.findall(r"GOAL_PLUS_STATIC_SCORE\s*=\s*([0-9]+(?:\.[0-9]+)?)", combined)]
    score = max(scores, default=1.0 if not errors else 0.0)
    payload = {
        "static_score": score,
        "valid": not errors,
        "npu_executed": False,
        "errors": errors,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
