#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from goal_plus.domain.goal_commands import (  # noqa: E402
    render_goal_plus_command_config_markdown,
    render_goal_plus_command_usage,
)


DOC_START = "<!-- BEGIN GENERATED GOAL PLUS COMMAND CONFIG -->"
DOC_END = "<!-- END GENERATED GOAL PLUS COMMAND CONFIG -->"
PI_START = "\t// BEGIN GENERATED GOAL PLUS COMMAND CONFIG"
PI_END = "\t// END GENERATED GOAL PLUS COMMAND CONFIG"


def _replace_block(text: str, start: str, end: str, generated: str) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise RuntimeError(f"expected exactly one generated block: {start}")
    before, remainder = text.split(start, 1)
    _old, after = remainder.split(end, 1)
    return f"{before}{start}\n{generated.rstrip()}\n{end}{after}"


def _expected_files() -> dict[Path, str]:
    docs_path = ROOT / "docs" / "api.md"
    docs = _replace_block(
        docs_path.read_text(encoding="utf-8"),
        DOC_START,
        DOC_END,
        render_goal_plus_command_config_markdown(),
    )

    pi_path = ROOT / "assets/pi/extensions/goal-plus.ts"
    usage = render_goal_plus_command_usage()
    pi_generated = (
        "\tconst GOAL_PLUS_COMMAND_CONFIG_USAGE = "
        f"{json.dumps(usage, ensure_ascii=False)};"
    )
    pi = _replace_block(
        pi_path.read_text(encoding="utf-8"),
        PI_START,
        PI_END,
        pi_generated,
    )
    return {docs_path: docs, pi_path: pi}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Goal Plus host-command config docs and usage."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args()

    stale: list[Path] = []
    for path, expected in _expected_files().items():
        if path.read_text(encoding="utf-8") == expected:
            continue
        stale.append(path)
        if args.write:
            path.write_text(expected, encoding="utf-8")

    if args.check and stale:
        for path in stale:
            print(f"stale generated command config: {path.relative_to(ROOT)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
