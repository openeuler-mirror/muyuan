"""Check extracted inputs/oracle and run the upstream answer outside the task tree."""

import argparse
import importlib
import hashlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from unittest.mock import patch

import torch
from golden import ScalarSpec
from golden.runner import RunResult


def materialize(specs):
    return {s.name: s.to_python() if isinstance(s, ScalarSpec) else s.create_tensor() for s in specs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--case", default="default")
    parser.add_argument("--reference-only", action="store_true")
    args = parser.parse_args()
    manifest = json.loads((args.task / "provenance.json").read_text())
    repo = Path(subprocess.check_output(["git", "-C", str(args.upstream.resolve().parent), "rev-parse", "--show-toplevel"], text=True).strip())
    assert args.upstream.resolve() == repo / manifest["source"], "different upstream source"
    for name, digest in manifest["source_files"].items():
        assert hashlib.sha256((repo / name).read_bytes()).hexdigest() == digest, f"upstream changed: {name}"
    sys.argv = [str(args.upstream), *manifest["specialize"]]
    sys.path.insert(0, str(args.upstream.resolve().parent))
    upstream = importlib.import_module(args.upstream.stem)
    sys.path.insert(0, str(args.task.resolve()))
    import contract
    import verify

    case = contract.CASES[args.case]
    outputs = set(contract.make_comparisons(case))
    torch.manual_seed(case["seed"])
    original_specs = getattr(upstream, manifest["inputs"])(**case["inputs"])
    original = materialize(original_specs)
    torch.manual_seed(case["seed"])
    extracted_specs = contract.make_specs(case)
    extracted = materialize(extracted_specs)
    assert list(original) == list(extracted), "input names/order changed"
    for left, right in zip(original_specs, extracted_specs, strict=True):
        assert getattr(left, "resident", None) == getattr(right, "resident", None), left.name
    for name in original:
        if isinstance(original[name], torch.Tensor):
            assert original[name].dtype == extracted[name].dtype, name
            torch.testing.assert_close(original[name], extracted[name], rtol=0, atol=0, msg=name)
        else:
            assert original[name] == extracted[name], name
    print("INPUTS_BITWISE_EQUAL", flush=True)
    getattr(upstream, manifest["reference"])(original)
    contract.reference(extracted)
    for name in outputs:
        torch.testing.assert_close(original[name], extracted[name], rtol=0, atol=0, msg=name)
    print("REFERENCE_BITWISE_EQUAL " + ",".join(sorted(outputs)), flush=True)

    comparisons = verify.guarded_comparisons(contract.make_comparisons(case), set())
    kwargs = dict(actual_outputs=extracted, expected_outputs=original, inputs=extracted,
                  rtol=contract.RTOL, atol=contract.ATOL)
    for name, compare in comparisons.items():
        expected = original[name]
        assert compare(expected, expected, **kwargs)[0], name
        assert not compare(torch.zeros_like(expected), expected, **kwargs)[0], name
        if expected.is_floating_point():
            assert not compare(torch.full_like(expected, float("nan")), expected, **kwargs)[0], name
        assert not compare(expected.reshape(-1)[:1], expected, **kwargs)[0], name
    print("WRONG_OUTPUTS_REJECTED", flush=True)
    del original, extracted, original_specs, extracted_specs, kwargs
    fn = getattr(upstream, manifest["entry"])
    with patch.object(verify, "run", return_value=RunResult(passed=True)), patch.object(contract, "make_specs", return_value=[]):
        assert not verify.check_case(fn, case)["valid"], "missing output checks were accepted"
    print("MISSING_OUTPUT_CHECKS_REJECTED", flush=True)
    if not args.reference_only:
        os.environ["PYPTO_BENCH"] = "0"
        with tempfile.TemporaryDirectory(prefix="pypto-acceptance-") as build:
            os.environ["PYPTO_PROG_BUILD_DIR"] = build
            result = verify.check_case(fn, case)
            print(json.dumps(result), flush=True)
            assert result["valid"], result
        print("UPSTREAM_KERNEL_PASSED_TASK_VERIFIER", flush=True)


if __name__ == "__main__":
    main()
