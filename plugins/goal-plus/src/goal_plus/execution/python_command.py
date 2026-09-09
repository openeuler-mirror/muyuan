"""Preserve the host interpreter's import isolation in internal Python children."""

from collections.abc import Sequence
import sys


def module_arguments(module: str, arguments: Sequence[str]) -> list[str]:
    # Source-driven tests retain their explicit PYTHONPATH. Installed hosts and
    # the editable Pi launcher start with -I, which their own children inherit.
    # Do not change Python settings for user verifiers or other external tools.
    return [*(["-I"] if sys.flags.isolated else []), "-m", module, *arguments]
