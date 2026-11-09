"""Bounded project-file reads for the optional local Codex installation.

The operator selects the root at install time. No shell, writes, directory
discovery, symlink traversal, or model-selected roots are exposed.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
PRIVATE = {".git", ".codex", ".agents", ".bate-codex", ".meta-tool", ".pi", ".aws", "credentials", "secrets"}


def read_range(root: Path, path: str, offset: int, limit: int) -> dict:
    if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= 300:
        raise ValueError("Invalid line range")
    if not isinstance(path, str) or not path or len(path) > 4096:
        raise ValueError("Invalid path")
    root = root.resolve(strict=True)
    requested = Path(path)
    if requested.is_absolute():
        requested = requested.relative_to(root)
    parts = requested.parts
    if not parts or any(p in PRIVATE or p == ".." or p == ".env" or p.startswith(".env.") for p in parts):
        raise ValueError("Path is outside the readable project scope")
    # Open each component relative to an already-open directory. Reject symlinks
    # including races between validation and open; never block on a FIFO/device.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_fd
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
                raise ValueError("Only regular text files up to 8 MiB are readable")
            data = stream.read(MAX_FILE_BYTES + 1)
    finally:
        os.close(directory)
    if len(data) > MAX_FILE_BYTES or b"\0" in data:
        raise ValueError("File is too large or binary")
    lines = data.decode("utf-8").splitlines(keepends=True)
    selected = "".join(lines[offset - 1:offset - 1 + limit]).encode("utf-8")
    return {"path": str(requested), "offset": offset, "limit": limit,
            "text": selected[:MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore"),
            "truncated": len(selected) > MAX_OUTPUT_BYTES, "total_lines": len(lines)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if not root.is_dir():
        parser.error("root must be a directory")
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    server = FastMCP("bate-project-files")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
    def read(path: str, offset: int, limit: int) -> dict:
        """Read a known UTF-8 project-file range: at most 300 lines / 64 KiB."""
        try:
            return read_range(root, path, offset, limit)
        except (OSError, ValueError):
            raise ValueError("Project read rejected: check path, text format and size limits") from None

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
