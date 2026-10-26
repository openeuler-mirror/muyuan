"""Bind default Python verifier commands to the trusted runtime interpreter."""

import sys
from collections.abc import Sequence


def verifier_argv(command: Sequence[str]) -> list[str]:
    if command and command[0] in {"python", "python3"}:
        # Resolving the symlink would discard the installed virtual environment.
        return [sys.executable, *command[1:]]
    return list(command)
