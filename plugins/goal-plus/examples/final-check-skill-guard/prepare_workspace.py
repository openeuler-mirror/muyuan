#!/usr/bin/env python3
"""Materialize the final-check example as an isolated Git workspace."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess


EXAMPLE_ROOT = Path(__file__).resolve().parent
PLUGIN_ROOT = EXAMPLE_ROOT.parents[1]
TEMPLATE = EXAMPLE_ROOT / "workspace-template"
DEFAULT_OUTPUT = PLUGIN_ROOT / ".tmp" / "final-check-skill-guard-workspace"
PROMPTS = ("codex-goal-prompt.md", "pi-goal-prompt.md")


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        text=True,
        capture_output=True,
    )


def materialize(output: Path) -> Path:
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(
            f"output already exists: {output}; choose a new --output path"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE, output)
    for prompt in PROMPTS:
        shutil.copy2(EXAMPLE_ROOT / prompt, output / prompt)
    _git(output, "init", "-q")
    _git(output, "add", ".")
    _git(
        output,
        "-c",
        "user.name=goal-plus-example",
        "-c",
        "user.email=goal-plus-example@example.invalid",
        "commit",
        "-q",
        "-m",
        "materialize final-check skill guard fixture",
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    print(materialize(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
