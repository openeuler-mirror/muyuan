#!/usr/bin/env python3
"""Read-only validity and ranking verifier for the single-core Torch target."""

from __future__ import annotations

import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = 5


class VerifierEnvironment:
    def __init__(self) -> None:
        configured_tmpdir = os.environ.get("GOAL_PLUS_VERIFIER_TMPDIR") or os.environ.get(
            "TMPDIR"
        )
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        if configured_tmpdir:
            self.tmpdir = Path(configured_tmpdir)
        else:
            fallback = Path(
                os.environ.get("GOAL_PLUS_TMPDIR") or Path.cwd() / ".tmp"
            ).expanduser()
            fallback.mkdir(parents=True, exist_ok=True)
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix="goal-plus-torch-cpu-", dir=fallback
            )
            self.tmpdir = Path(self._temporary_directory.name)
        self.extension_dir = self.tmpdir / "torch_extensions"

    def subprocess_env(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["MAX_JOBS"] = "1"
        environment["TORCH_EXTENSIONS_DIR"] = str(self.extension_dir)
        return environment


VERIFIER_ENVIRONMENT = VerifierEnvironment()


def run_json(script: str) -> dict:
    completed = subprocess.run(
        [sys.executable, script],
        cwd=ROOT,
        env=VERIFIER_ENVIRONMENT.subprocess_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{script} exited {completed.returncode}: {completed.stderr[-2000:]}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"{script} produced no JSON output")
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{script} final output was not JSON: {lines[-1]!r}") from error
    if not isinstance(result, dict):
        raise RuntimeError(f"{script} final JSON was not an object")
    return result


def check_validity() -> None:
    result = run_json("verify.py")
    if result.get("valid") is not True or result.get("torch_num_threads") != 1:
        raise RuntimeError(f"verify.py rejected candidate: {result}")


def rank() -> float:
    check_validity()
    scores: list[float] = []
    for _ in range(SAMPLES):
        result = run_json("benchmark.py")
        score = result.get("tokens_per_second")
        if result.get("valid") is not True or result.get("torch_num_threads") != 1:
            raise RuntimeError(f"benchmark.py rejected candidate: {result}")
        if (
            not isinstance(score, (int, float))
            or isinstance(score, bool)
            or not math.isfinite(score)
        ):
            raise RuntimeError(
                f"benchmark.py emitted invalid tokens_per_second: {result}"
            )
        scores.append(float(score))
    return float(statistics.median(scores))


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"--validate", "--rank"}:
        raise SystemExit("usage: torch_cpu_tokens.py --validate|--rank")
    try:
        if sys.argv[1] == "--validate":
            check_validity()
            result = {"valid": True}
        else:
            result = {"tokens_per_second": rank()}
    except Exception as error:
        print(f"torch CPU verifier failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
