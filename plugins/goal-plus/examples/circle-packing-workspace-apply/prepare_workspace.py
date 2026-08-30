#!/usr/bin/env python3
"""Prepare an ignored, independent Git source for the apply-back example."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess


EXAMPLE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = EXAMPLE_DIR.parent / "circle-packing" / "workspace-template"
DEFAULT_DESTINATION = EXAMPLE_DIR / "workspace"
BASELINE_COMMIT_MESSAGE = "circle packing disposable workspace baseline"
WORKSPACE_GITIGNORE = """.gp/
.tmp/
__pycache__/
*.py[cod]
"""


def _run_git(destination: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=destination,
        check=True,
        capture_output=True,
        text=True,
    )


def prepare_workspace(destination: Path) -> Path:
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(
            f"destination already exists; move it aside before retrying: {destination}"
        )

    destination.mkdir(parents=True)
    verifier_destination = destination / ".goal-plus-verifiers"
    verifier_destination.mkdir()

    shutil.copy2(TEMPLATE_DIR / "initial_program.py", destination)
    shutil.copy2(
        TEMPLATE_DIR / ".goal-plus-verifiers" / "circle_packing_score.py",
        verifier_destination,
    )
    shutil.copy2(EXAMPLE_DIR / "search-spec.json", destination)
    shutil.copy2(EXAMPLE_DIR / "workspace-README.md", destination / "README.md")
    (destination / ".gitignore").write_text(
        WORKSPACE_GITIGNORE,
        encoding="utf-8",
    )

    _run_git(destination, "init", "-q", "-b", "main")
    _run_git(destination, "add", "--all")
    _run_git(
        destination,
        "-c",
        "user.name=goal-plus-example",
        "-c",
        "user.email=goal-plus-example@example.invalid",
        "commit",
        "-q",
        "--no-verify",
        "-m",
        BASELINE_COMMIT_MESSAGE,
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_DESTINATION,
        help="new workspace path (default: examples/.../workspace)",
    )
    args = parser.parse_args()

    try:
        destination = prepare_workspace(args.destination)
    except (FileExistsError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"error: {exc}\n")

    print(f"Prepared independent Git workspace: {destination}")
    print(f"Next: cd {destination}")
    print("Then start Pi and paste ../pi-goal-prompt.md when using the default path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
