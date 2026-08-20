#!/usr/bin/env python3
"""Synchronize installable Codex plugin skills from project-local assets."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS = (
    "goal-plus",
    "goal-plus-with-final-check",
    "search",
)


def sync(*, check: bool) -> list[Path]:
    stale: list[Path] = []
    for name in SKILLS:
        source = ROOT / ".codex" / "skills" / name / "SKILL.md"
        destination = ROOT / "skills" / name / "SKILL.md"
        source_bytes = source.read_bytes()
        if destination.is_file() and destination.read_bytes() == source_bytes:
            continue
        stale.append(destination)
        if not check:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report stale plugin skills without modifying files.",
    )
    args = parser.parse_args()
    stale = sync(check=args.check)
    if args.check and stale:
        for path in stale:
            print(path.relative_to(ROOT))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
