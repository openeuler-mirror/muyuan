#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import runpy
import time


CALLS = 200


def main() -> int:
    score_batch = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scoring.py")
    )["score_batch"]
    values = [float(value) for value in range(64)]
    score_batch(values)
    started_at = time.perf_counter()
    for _ in range(CALLS):
        score_batch(values)
    elapsed = time.perf_counter() - started_at
    print(
        json.dumps(
            {"valid": True, "calls_per_second": CALLS / elapsed},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
