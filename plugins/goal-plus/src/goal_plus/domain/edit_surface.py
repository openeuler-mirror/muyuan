"""Pure edit-surface matching shared by settlement and native write admission."""

from collections.abc import Sequence
from fnmatch import fnmatch
from os import sep


def path_matches(path: str, patterns: Sequence[str]) -> bool:
    normalized = path.replace(sep, "/")
    for pattern in patterns:
        pat = pattern.replace(sep, "/")
        if normalized == pat or fnmatch(normalized, pat):
            return True
        if pat.endswith("/") and normalized.startswith(pat):
            return True
        if normalized.startswith(pat.rstrip("/") + "/"):
            return True
    return False
