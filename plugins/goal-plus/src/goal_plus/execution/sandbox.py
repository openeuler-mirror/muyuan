"""Resolve trusted child-process sandbox policies into OS launch commands."""

from __future__ import annotations

import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from goal_plus.ports.sandbox import ProcessSandbox
from goal_plus.temp import temporary_root


class SandboxUnavailable(RuntimeError):
    """The requested protected execution backend is not usable on this host."""


def resolve_sandbox_command(
    command: Sequence[str],
    *,
    cwd: Path,
    sandbox: ProcessSandbox | None,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    """Resolve one command without silently weakening a requested policy."""

    if sandbox is None:
        return tuple(command)
    _validate_paths(cwd, sandbox)
    if sandbox.backend == "native":
        return tuple(command)
    platform = platform or sys.platform
    if platform == "darwin":
        # Like Codex, use the OS binary rather than a workspace-controlled PATH.
        executable = which("/usr/bin/sandbox-exec")
        if executable is None:
            raise SandboxUnavailable("macOS protected execution requires sandbox-exec")
        return _seatbelt_command(executable, command, sandbox)
    if platform.startswith("linux"):
        executable = which("bwrap")
        if executable is None:
            raise SandboxUnavailable("Linux protected execution requires bubblewrap (bwrap)")
        return _bubblewrap_command(executable, command, cwd, sandbox)
    raise SandboxUnavailable(f"no protected execution backend for platform {platform!r}")


def _normal_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _validate_paths(cwd: Path, sandbox: ProcessSandbox) -> None:
    _unique_writable_roots(sandbox)
    cwd = _normal_path(cwd)
    if not any(
        cwd.is_relative_to(_normal_path(root))
        for root in (sandbox.workspace, *sandbox.writable_roots)
    ):
        raise ValueError(f"sandbox cwd {cwd} is outside its declared roots")


def prepare_sandbox_environment(
    sandbox: ProcessSandbox | None, *, cwd: Path, environment: dict[str, str], cleanup: ExitStack,
) -> ProcessSandbox | None:
    """Give both outer launch paths an owned scratch directory and matching policy.

    The caller owns cleanup until the direct process exits. Scratch preparation
    failures propagate; sandbox failures never retry an unprotected command.
    """
    if sandbox is None or sandbox.backend == "native":
        return sandbox
    _validate_paths(cwd, sandbox)
    # Keep host scratch beside the active workspace. The process-wide /tmp
    # namespace is often inode constrained on shared hosts and is not a
    # reliable default for long-running candidate work.
    temporary = tempfile.TemporaryDirectory(
        prefix="goal-plus-", dir=str(temporary_root(cwd))
    )
    directory = _normal_path(Path(cleanup.enter_context(temporary)))
    # Helpers inside the child must use this owned writable directory, not its
    # read-only parent root selected by the host's operational override.
    for key in ("TMPDIR", "TMP", "TEMP", "GOAL_PLUS_TMPDIR"):
        environment[key] = str(directory)
    return replace(sandbox, writable_roots=(*sandbox.writable_roots, directory))


def _unique_writable_roots(sandbox: ProcessSandbox) -> tuple[Path, ...]:
    roots: list[Path] = []
    if sandbox.mode == "workspace-write":
        roots.append(_normal_path(sandbox.workspace))
    roots.extend(_normal_path(path) for path in sandbox.writable_roots)
    seen: set[Path] = set()
    result: list[Path] = []
    for root in roots:
        if root == Path("/"):
            raise ValueError("sandbox writable root cannot be filesystem root")
        if root not in seen:
            seen.add(root)
            result.append(root)
    return tuple(result)


def _sbpl_string(path: Path) -> str:
    value = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def _seatbelt_command(
    executable: str, command: Sequence[str], sandbox: ProcessSandbox,
) -> tuple[str, ...]:
    roots = _unique_writable_roots(sandbox)
    allowed_paths = [f"(subpath {_sbpl_string(root)})" for root in roots]
    # Keep standard character devices usable without granting filesystem writes.
    allowed_paths.extend(
        f"(literal {_sbpl_string(Path(path))})"
        for path in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom", "/dev/ptmx")
    )
    allowed = " ".join(allowed_paths)
    profile = (
        '(version 1) (allow default) '
        f"(deny file-write* (require-not (require-any {allowed})))"
    )
    if sandbox.network == "deny":
        profile += " (deny network*)"
    return (executable, "-p", profile, *command)


def _bubblewrap_command(
    executable: str, command: Sequence[str], cwd: Path, sandbox: ProcessSandbox,
) -> tuple[str, ...]:
    roots = _unique_writable_roots(sandbox)
    cwd = _normal_path(cwd)
    args: list[str] = [
        executable, "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
        "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
    ]
    if sandbox.mode == "read-only":
        # The private /tmp must not hide a read-only workspace hosted there.
        workspace = str(_normal_path(sandbox.workspace))
        args.extend(("--ro-bind", workspace, workspace))
    for root in roots:
        args.extend(("--bind", str(root), str(root)))
    if sandbox.network == "deny":
        args.append("--unshare-net")
    args.extend(("--chdir", str(cwd), "--", *command))
    return tuple(args)
