"""Pure source-relative snapshot path projections."""

from pathlib import PurePosixPath


def thinkthread_snapshot_path(source_relative_path: str, path: str) -> str:
    source = PurePosixPath(source_relative_path)
    child = PurePosixPath(path)
    if child.is_absolute() or ".." in child.parts:
        raise ValueError("verifier path escapes the ThinkThread source_path")
    combined = source if child.as_posix() == "." else source / child
    return combined.as_posix() or "."


def thinkthread_logical_changed_files(
    source_relative_path: str,
    changed_files: list[str],
) -> list[str]:
    source = PurePosixPath(source_relative_path)
    if source.as_posix() == ".":
        return sorted(changed_files)
    prefix = f"{source.as_posix().rstrip('/')}/"
    parent = "../" * len(source.parts)
    return sorted(
        path.removeprefix(prefix) if path.startswith(prefix) else f"{parent}{path}"
        for path in changed_files
    )
