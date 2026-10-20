"""Run the task's frozen correctness contract on the CPU simulator."""

import argparse
import importlib
import json
import os
import tempfile
from pathlib import Path

import torch
from golden import run

import contract


def _temporary_root() -> Path:
    root = Path(
        os.environ.get("GOAL_PLUS_VERIFIER_TMPDIR")
        or os.environ.get("GOAL_PLUS_TMPDIR")
        or Path.cwd() / ".tmp"
    ).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def guarded_comparisons(comparisons, checked):
    def wrap(name, compare):
        def check(actual, expected, **kwargs):
            checked.add(name)
            if actual.shape != expected.shape or actual.dtype != expected.dtype:
                return False, f"{name}: shape or dtype mismatch"
            return compare(actual, expected, **kwargs)
        return check
    return {name: wrap(name, compare) for name, compare in comparisons.items()}


def check_case(fn, case):
    torch.manual_seed(case["seed"])
    comparisons = contract.make_comparisons(case)
    if not comparisons:
        raise ValueError("The contract must declare checked outputs")
    checked = set()
    result = run(
        fn=fn, specs=contract.make_specs(case), golden_fn=contract.reference,
        config=contract.RUN_CONFIG, rtol=contract.RTOL, atol=contract.ATOL,
        compare_fn=guarded_comparisons(comparisons, checked),
    )
    missing = sorted(comparisons.keys() - checked)
    return {"valid": bool(result.passed and not missing), "checked": sorted(checked),
            "missing": missing, "error": result.error}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["all", *contract.CASES], default="all")
    args = parser.parse_args()
    results = {}
    os.environ["PYPTO_BENCH"] = "0"
    try:
        fn = getattr(importlib.import_module("kernel"), contract.ENTRY)
        with tempfile.TemporaryDirectory(
            prefix="pypto-verify-", dir=_temporary_root()
        ) as build:
            os.environ["PYPTO_PROG_BUILD_DIR"] = build
            for name in contract.CASES if args.case == "all" else [args.case]:
                print(f"[CASE] {name}", flush=True)
                results[name] = check_case(fn, contract.CASES[name])
                if not results[name]["valid"]:
                    break
        valid = bool(results) and all(item["valid"] for item in results.values())
        report = {"valid": valid, "score": int(valid), "cases": results}
    except Exception as exc:
        report = {"valid": False, "score": 0, "cases": results, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
