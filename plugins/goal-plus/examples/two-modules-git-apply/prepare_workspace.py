#!/usr/bin/env python3
"""Create a disposable writable Git source for the two-module example."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

EXAMPLE_ROOT = Path(__file__).resolve().parent
TEMPLATE_ROOT = EXAMPLE_ROOT.parent / "two-modules-thinkthread-apply" / "workspace-template"
SPEC_PATH = EXAMPLE_ROOT / "search-spec.json"
DEFAULT_WORKSPACE = EXAMPLE_ROOT / "workspace"
MARKER_NAME = ".two-modules-git-example"


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(workspace), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def materialize_workspace(destination: Path, *, reset: bool = False) -> Path:
    destination = destination.resolve()
    marker = destination / MARKER_NAME
    if destination.exists():
        if not reset:
            raise RuntimeError(f"destination already exists: {destination}; use --reset to replace it")
        if not marker.is_file():
            raise RuntimeError(f"refusing to reset unrecognized destination: {destination}")
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE_ROOT, destination)
    shutil.copy2(SPEC_PATH, destination / SPEC_PATH.name)
    marker.write_text(MARKER_NAME + "\n", encoding="utf-8")
    try:
        subprocess.run(["git", "init", "-q", str(destination)], check=True, capture_output=True, text=True)
        _git(destination, "add", "-A")
        _git(
            destination,
            "-c", "user.name=goal-plus-example",
            "-c", "user.email=goal-plus@example.invalid",
            "commit", "-q", "--no-verify", "-m", "two-module baseline",
        )
        if _git(destination, "status", "--porcelain=v1", "--untracked-files=all"):
            raise RuntimeError("generated workspace is not clean")
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    try:
        destination = materialize_workspace(args.destination, reset=args.reset)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(f"workspace={destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
