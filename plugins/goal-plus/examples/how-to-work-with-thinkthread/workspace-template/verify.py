#!/usr/bin/env python3
"""Verify the deterministic Goal Mode output."""

from __future__ import annotations

import json
from pathlib import Path


EXPECTED = "Hello from ThinkThread!\n"


def main() -> int:
    actual = (Path(__file__).resolve().parent / "greeting.txt").read_text(
        encoding="utf-8"
    )
    if actual != EXPECTED:
        print(
            json.dumps(
                {
                    "status": "fail",
                    "expected": EXPECTED,
                    "actual": actual,
                },
                ensure_ascii=True,
            )
        )
        return 1
    print(json.dumps({"status": "pass", "greeting": actual.rstrip("\n")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
